"""Tests for the interactive duplicate-resolution phase: execute_dupe_resolutions
in cleaner.py and the `ocs fix --only dupes` CLI flow.

Mirrors the fixture style of test_mutations.py (small helpers rebuilt here
rather than imported, per instructions) — a realistic tmp_path profile tree,
never touching the user's real OrcaSlicer profiles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from orcaslicer_cleaner import loader
from orcaslicer_cleaner.cleaner import DupeResolution, execute_dupe_resolutions
from orcaslicer_cleaner.cli import cli
from orcaslicer_cleaner.models import ProfileCategory

MACHINE_A = "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm"
MACHINE_B = "Doomcube - WWBMG - TeaKettle - 0.4mm"


def write_profile(root: Path, category: str, name: str, settings: dict, updated_time: int = 1700000000) -> None:
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"{name}.json").write_text(
        json.dumps(settings, indent=4) + "\n", encoding="utf-8"
    )
    (cat_dir / f"{name}.info").write_text(
        f"sync_info = \nuser_id = 123\nsetting_id = \nbase_id = \nupdated_time = {updated_time}\n",
        encoding="utf-8",
    )


@pytest.fixture
def console() -> Console:
    return Console(file=open("/dev/null", "w"), force_terminal=False)


@pytest.fixture
def profile_tree(tmp_path: Path) -> Path:
    """user/<account>/{machine,filament,process}/ with linked profiles."""
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"
    for machine in (MACHINE_A, MACHINE_B):
        write_profile(root, "machine", machine, {"name": machine, "printer_settings_id": machine})
    return user_dir


def load_tree(user_dir: Path):
    merged = {cat: [] for cat in ProfileCategory}
    for root in loader.discover_profile_dirs(user_dir):
        loaded = loader.load_profiles(root)
        for cat in ProfileCategory:
            merged[cat].extend(loaded[cat])
    return merged


def get_profile(profiles, category: ProfileCategory, name: str):
    for p in profiles[category]:
        if p.name == name:
            return p
    raise AssertionError(f"profile not found: {name}")


def read_json(root: Path, category: str, name: str) -> dict:
    return json.loads((root / category / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# execute_dupe_resolutions
# ---------------------------------------------------------------------------


class TestExecuteDupeResolutions:
    def test_archives_losers_and_leaves_keeper(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Keep", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Old", {"compatible_printers": [MACHINE_A]}, updated_time=100)
        profiles = load_tree(profile_tree)
        keep = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep")
        loser = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Old")
        backup_root = tmp_path / "_backup"

        resolutions = [DupeResolution(keep=keep, archive=[loser])]
        assert execute_dupe_resolutions(console, resolutions, backup_root) == 1

        # Loser archived
        assert not loser.json_path.exists()
        assert not loser.info_path.exists()
        # Keeper untouched
        assert keep.json_path.exists()
        assert read_json(root, "filament", "PLA - Keep")["compatible_printers"] == [MACHINE_A]

        backup_dir = next(backup_root.iterdir())
        assert (backup_dir / "filament" / "PLA - Old.json").exists()

    def test_merge_unions_compatible_printers_and_backs_up_original(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Keep", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Dupe", {"compatible_printers": [MACHINE_B]}, updated_time=100)
        profiles = load_tree(profile_tree)
        keep = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep")
        loser = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Dupe")
        backup_root = tmp_path / "_backup"

        pre_merge_content = read_json(root, "filament", "PLA - Keep")

        resolutions = [
            DupeResolution(
                keep=keep, archive=[loser],
                merged_printers=sorted([MACHINE_A, MACHINE_B]),
            )
        ]
        assert execute_dupe_resolutions(console, resolutions, backup_root) == 1

        updated = read_json(root, "filament", "PLA - Keep")
        assert sorted(updated["compatible_printers"]) == sorted([MACHINE_A, MACHINE_B])

        backup_dir = next(backup_root.iterdir())
        backed_up = json.loads((backup_dir / "filament" / "PLA - Keep.json").read_text())
        assert backed_up == pre_merge_content
        assert backed_up["compatible_printers"] == [MACHINE_A]

    def test_empty_merged_printers_not_written_and_warns(self, profile_tree, console, tmp_path, capsys):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Keep", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Dupe", {"compatible_printers": [MACHINE_A]}, updated_time=100)
        profiles = load_tree(profile_tree)
        keep = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep")
        loser = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Dupe")
        backup_root = tmp_path / "_backup"

        warn_console = Console(record=True, force_terminal=False)
        resolutions = [DupeResolution(keep=keep, archive=[loser], merged_printers=[])]
        assert execute_dupe_resolutions(warn_console, resolutions, backup_root) == 1

        # Keeper json unchanged
        assert read_json(root, "filament", "PLA - Keep")["compatible_printers"] == [MACHINE_A]
        output = warn_console.export_text()
        assert "Warning" in output

    def test_one_failing_resolution_does_not_block_others(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Keep1", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Old1", {"compatible_printers": [MACHINE_A]}, updated_time=100)
        write_profile(root, "filament", "PLA - Keep2", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Old2", {"compatible_printers": [MACHINE_A]}, updated_time=100)
        profiles = load_tree(profile_tree)
        keep1 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep1")
        old1 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Old1")
        keep2 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep2")
        old2 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Old2")
        backup_root = tmp_path / "_backup"

        # Simulate a failure on the first resolution: its keeper's json is
        # already gone before we execute (e.g. removed out-of-band), so the
        # merge write for it raises/handles gracefully, but the archive of
        # the second group's loser must still happen.
        keep1.json_path.unlink()
        keep1.info_path.unlink()

        resolutions = [
            DupeResolution(keep=keep1, archive=[old1], merged_printers=[MACHINE_A, MACHINE_B]),
            DupeResolution(keep=keep2, archive=[old2]),
        ]
        resolved = execute_dupe_resolutions(console, resolutions, backup_root)

        # Second resolution succeeded even though the first's merge write failed
        assert resolved == 2  # archive succeeds for both; merge write for #1 just logs "Missing"
        assert not old1.json_path.exists()
        assert not old2.json_path.exists()
        assert keep2.json_path.exists()
        assert read_json(root, "filament", "PLA - Keep2")["compatible_printers"] == [MACHINE_A]

    def test_one_group_raises_does_not_block_others(self, profile_tree, console, tmp_path, monkeypatch):
        """A hard failure (exception) in one group's processing must not
        prevent later groups in the same batch from executing."""
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Keep1", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Old1", {"compatible_printers": [MACHINE_A]}, updated_time=100)
        write_profile(root, "filament", "PLA - Keep2", {"compatible_printers": [MACHINE_A]}, updated_time=200)
        write_profile(root, "filament", "PLA - Old2", {"compatible_printers": [MACHINE_A]}, updated_time=100)
        profiles = load_tree(profile_tree)
        keep1 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep1")
        old1 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Old1")
        keep2 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Keep2")
        old2 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Old2")
        backup_root = tmp_path / "_backup"

        import orcaslicer_cleaner.cleaner as cleaner_mod

        original_archive = cleaner_mod._archive_profile
        call_count = {"n": 0}

        def flaky_archive(profile, backup_dir):
            call_count["n"] += 1
            if profile is old1:
                raise RuntimeError("simulated failure")
            return original_archive(profile, backup_dir)

        monkeypatch.setattr(cleaner_mod, "_archive_profile", flaky_archive)

        resolutions = [
            DupeResolution(keep=keep1, archive=[old1]),
            DupeResolution(keep=keep2, archive=[old2]),
        ]
        resolved = execute_dupe_resolutions(console, resolutions, backup_root)

        assert resolved == 1  # only the second group succeeded
        assert old1.json_path.exists()  # never archived due to failure
        assert not old2.json_path.exists()


# ---------------------------------------------------------------------------
# CLI: fix --only dupes
# ---------------------------------------------------------------------------


class TestFixDupesCli:
    def _run(self, profile_tree, args, input_text=None):
        runner = CliRunner()
        return runner.invoke(
            cli,
            ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", "fix", "--only", "dupes", *args],
            input=input_text,
        )

    def test_mergeable_group_end_to_end(self, profile_tree):
        root = profile_tree / "1234567890"
        write_profile(
            root, "filament", "PETG - 3DO (Doomcube - LGX Lite Pro - TeaKettle - 0.4mm)",
            {"compatible_printers": [MACHINE_A], "filament_settings_id": "x", "temperature": 240},
            updated_time=200,
        )
        write_profile(
            root, "filament", "PETG - 3DO (Doomcube - WWBMG - TeaKettle - 0.4mm)",
            {"compatible_printers": [MACHINE_B], "filament_settings_id": "x", "temperature": 240},
            updated_time=100,
        )

        # Choose keeper #1 (the most-recently-updated one, which sorts first
        # since find_duplicates groups by content hash — verify via output).
        result = self._run(profile_tree, [], input_text="1\ny\n")
        assert result.exit_code == 0, result.output

        profiles = load_tree(profile_tree)
        filament = profiles[ProfileCategory.FILAMENT]
        remaining_names = {p.name for p in filament}

        # Exactly one of the two mergeable profiles remains
        candidates = {
            "PETG - 3DO (Doomcube - LGX Lite Pro - TeaKettle - 0.4mm)",
            "PETG - 3DO (Doomcube - WWBMG - TeaKettle - 0.4mm)",
        }
        kept = candidates & remaining_names
        archived = candidates - remaining_names
        assert len(kept) == 1
        assert len(archived) == 1

        kept_name = next(iter(kept))
        cp = read_json(root, "filament", kept_name)["compatible_printers"]
        assert sorted(cp) == sorted([MACHINE_A, MACHINE_B])

        # Archived pair actually gone from disk
        archived_name = next(iter(archived))
        assert not (root / "filament" / f"{archived_name}.json").exists()
        assert not (root / "filament" / f"{archived_name}.info").exists()

    def test_skip_leaves_both_profiles_untouched(self, profile_tree):
        root = profile_tree / "1234567890"
        write_profile(
            root, "filament", "PETG - 3DO (Doomcube - LGX Lite Pro - TeaKettle - 0.4mm)",
            {"compatible_printers": [MACHINE_A], "filament_settings_id": "x"},
            updated_time=200,
        )
        write_profile(
            root, "filament", "PETG - 3DO (Doomcube - WWBMG - TeaKettle - 0.4mm)",
            {"compatible_printers": [MACHINE_B], "filament_settings_id": "x"},
            updated_time=100,
        )

        result = self._run(profile_tree, [], input_text="s\n")
        assert result.exit_code == 0, result.output

        assert (root / "filament" / "PETG - 3DO (Doomcube - LGX Lite Pro - TeaKettle - 0.4mm).json").exists()
        assert (root / "filament" / "PETG - 3DO (Doomcube - WWBMG - TeaKettle - 0.4mm).json").exists()
