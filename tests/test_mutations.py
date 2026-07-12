"""End-to-end tests for the mutation layer: archive, remap, link fixes,
renames (with cascade + broadening), printer removal, and restore.

These operate on a realistic profile tree built in tmp_path — the goal is to
guarantee that every mutation backs up first, never corrupts a profile pair,
and that `ocs restore` can always undo what was done.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from orcaslicer_cleaner import loader
from orcaslicer_cleaner.cleaner import (
    RemapAction,
    execute_actions,
    execute_link_fixes,
    execute_printer_removal,
    execute_remap,
    find_printer_dependents,
    CleanAction,
)
from orcaslicer_cleaner.cli import cli
from orcaslicer_cleaner.fileops import load_manifest
from orcaslicer_cleaner.models import ProfileCategory
from orcaslicer_cleaner.standardizer import RenameAction, execute_renames

MACHINE_A = "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm"
MACHINE_B = "Doomcube - WWBMG - TeaKettle - 0.4mm"
MACHINE_C = "Positron - Sherpa Micro - TeaKettle - 0.6mm"


def write_profile(root: Path, category: str, name: str, settings: dict) -> None:
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"{name}.json").write_text(
        json.dumps(settings, indent=4) + "\n", encoding="utf-8"
    )
    (cat_dir / f"{name}.info").write_text(
        "sync_info = \nuser_id = 123\nsetting_id = \nbase_id = \nupdated_time = 1700000000\n",
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
    for machine in (MACHINE_A, MACHINE_B, MACHINE_C):
        write_profile(root, "machine", machine, {"name": machine, "printer_settings_id": machine})
    write_profile(
        root, "filament", "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)",
        {"compatible_printers": [MACHINE_A], "filament_settings_id": "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)"},
    )
    write_profile(
        root, "filament", "PLA - Shared",
        {"compatible_printers": [MACHINE_A, MACHINE_B]},
    )
    write_profile(
        root, "process", "0.20mm - Standard (Doomcube - 0.4mm)",
        {"compatible_printers": [MACHINE_A, MACHINE_B]},
    )
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
# Archive
# ---------------------------------------------------------------------------


class TestArchive:
    def test_archive_moves_pair_and_records_manifest(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        backup_root = tmp_path / "_backup"

        actions = [CleanAction(action="archive", profile=target, reason="test")]
        assert execute_actions(console, actions, backup_root) == 1

        assert not target.json_path.exists()
        assert not target.info_path.exists()

        backup_dir = next(backup_root.iterdir())
        assert (backup_dir / "filament" / "PLA - Shared.json").exists()
        assert (backup_dir / "filament" / "PLA - Shared.info").exists()

        manifest = load_manifest(backup_dir)
        assert manifest["filament/PLA - Shared.json"] == str(target.json_path)
        assert manifest["filament/PLA - Shared.info"] == str(target.info_path)

    def test_archive_collision_suffixes_and_manifest_maps_back(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        backup_root = tmp_path / "_backup"

        execute_actions(console, [CleanAction("archive", target, "test")], backup_root)
        backup_dir = next(backup_root.iterdir())

        # Recreate and archive again into the SAME backup dir
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Shared", {"compatible_printers": [MACHINE_A]})
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        from orcaslicer_cleaner.cleaner import _archive_profile
        _archive_profile(target, backup_dir)

        assert (backup_dir / "filament" / "PLA - Shared_1.json").exists()
        manifest = load_manifest(backup_dir)
        # Collision copy still maps back to the true original path
        assert manifest["filament/PLA - Shared_1.json"] == str(target.json_path)


class TestCleanDupesGuards:
    """Regression (2026-07-10 data loss): `clean --type dupes` must never
    auto-archive machines or identical-content profiles whose names are NOT
    variations of each other (cross-hardware twins may be the only copy
    serving another printer)."""

    def test_cross_named_twins_not_auto_archived(self, profile_tree, console, tmp_path):
        from orcaslicer_cleaner.cleaner import plan_cleanup
        from orcaslicer_cleaner.deduplicator import find_duplicates

        root = profile_tree / "1234567890"
        settings = {"temperature": ["255"], "compatible_printers": [MACHINE_A]}
        write_profile(root, "filament", "ABS - Fillamentum (WWBMG - TeaKettle - 0.4mm)", settings)
        write_profile(root, "filament", "ABS - Fillamentum (LGX Lite Pro - TeaKettle - 0.4mm)", settings)
        write_profile(root, "filament", "ABS - Fillamentum (LGX Lite Pro - TeaKettle - 0.4mm) - beta", settings)
        profiles = load_tree(profile_tree)

        actions = plan_cleanup([], find_duplicates(profiles), types=("dupes",))
        names = {a.profile.name for a in actions}
        # The beta variation is archivable; the cross-hardware twin is NOT.
        assert "ABS - Fillamentum (LGX Lite Pro - TeaKettle - 0.4mm) - beta" in names
        assert "ABS - Fillamentum (WWBMG - TeaKettle - 0.4mm)" not in names

    def test_machine_dupes_never_planned(self, profile_tree):
        from orcaslicer_cleaner.cleaner import plan_cleanup
        from orcaslicer_cleaner.deduplicator import find_duplicates

        profiles = load_tree(profile_tree)
        actions = plan_cleanup([], find_duplicates(profiles), types=("dupes",))
        assert not any(a.profile.category == ProfileCategory.MACHINE for a in actions)


# ---------------------------------------------------------------------------
# Remap
# ---------------------------------------------------------------------------


class TestRemap:
    def test_remap_replaces_reference_and_backs_up(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PETG - Old Ref", {"compatible_printers": ["Ghost Printer"]})
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PETG - Old Ref")
        backup_root = tmp_path / "_backup"

        actions = [RemapAction("Ghost Printer", [target], MACHINE_A)]
        assert execute_remap(console, actions, backup_root) == 1

        assert read_json(root, "filament", "PETG - Old Ref")["compatible_printers"] == [MACHINE_A]
        backup_dir = next(backup_root.iterdir())
        backed = json.loads((backup_dir / "filament" / "PETG - Old Ref.json").read_text())
        assert backed["compatible_printers"] == ["Ghost Printer"]

    def test_remap_removal_leaving_empty_archives_profile(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PETG - Ghost Only", {"compatible_printers": ["Ghost Printer"]})
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PETG - Ghost Only")
        backup_root = tmp_path / "_backup"

        actions = [RemapAction("Ghost Printer", [target], None)]
        assert execute_remap(console, actions, backup_root) == 1

        # Never written with empty compatible_printers — archived instead
        assert not (root / "filament" / "PETG - Ghost Only.json").exists()
        backup_dir = next(backup_root.iterdir())
        assert (backup_dir / "filament" / "PETG - Ghost Only.json").exists()


# ---------------------------------------------------------------------------
# Link fixes
# ---------------------------------------------------------------------------


class TestLinkFixes:
    def test_link_fix_updates_and_backs_up(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        backup_root = tmp_path / "_backup"

        assert execute_link_fixes(console, [(target, [MACHINE_A])], backup_root) == 1
        root = profile_tree / "1234567890"
        assert read_json(root, "filament", "PLA - Shared")["compatible_printers"] == [MACHINE_A]
        backup_dir = next(backup_root.iterdir())
        assert (backup_dir / "filament" / "PLA - Shared.json").exists()

    def test_link_fix_empty_list_archives_instead(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        backup_root = tmp_path / "_backup"

        assert execute_link_fixes(console, [(target, [])], backup_root) == 1
        assert not target.json_path.exists()


# ---------------------------------------------------------------------------
# Renames: cascade, broadening, collision safety
# ---------------------------------------------------------------------------


class TestRenames:
    def test_machine_rename_cascades_and_backs_up_cascaded_files(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        machine = get_profile(profiles, ProfileCategory.MACHINE, MACHINE_A)
        backup_root = tmp_path / "_backup"
        new_name = "Doomcube - LGX Lite Pro - TeaKettle - 0.6mm"

        actions = [RenameAction(machine, MACHINE_A, new_name)]
        assert execute_renames(console, actions, backup_root, all_profiles=profiles) == 1

        root = profile_tree / "1234567890"
        assert (root / "machine" / f"{new_name}.json").exists()
        assert not (root / "machine" / f"{MACHINE_A}.json").exists()

        # Dependent profiles updated
        cp = read_json(root, "filament", "PLA - Shared")["compatible_printers"]
        assert new_name in cp and MACHINE_A not in cp

        # Cascaded (but not renamed) profiles were backed up before mutation
        backup_dir = next(backup_root.iterdir())
        backed = json.loads((backup_dir / "filament" / "PLA - Shared.json").read_text())
        assert MACHINE_A in backed["compatible_printers"]

    def test_machine_rename_updates_json_internals(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        machine = get_profile(profiles, ProfileCategory.MACHINE, MACHINE_A)
        new_name = "Doomcube - LGX Lite Pro - TeaKettle - 0.8mm"

        execute_renames(
            console, [RenameAction(machine, MACHINE_A, new_name)],
            tmp_path / "_backup", all_profiles=profiles,
        )
        root = profile_tree / "1234567890"
        data = read_json(root, "machine", new_name)
        assert data["name"] == new_name
        assert data["printer_settings_id"] == new_name

    def test_rename_target_exists_is_skipped_without_partial_rename(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        # Existing profile occupies the target name
        write_profile(root, "filament", "ASA - 3DO (Doomcube)", {"compatible_printers": [MACHINE_A]})
        profiles = load_tree(profile_tree)
        src_name = "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)"
        src = get_profile(profiles, ProfileCategory.FILAMENT, src_name)

        renamed = execute_renames(
            console, [RenameAction(src, src_name, "ASA - 3DO (Doomcube)")],
            tmp_path / "_backup", all_profiles=profiles,
        )
        assert renamed == 0
        # No split profile: source pair untouched
        assert (root / "filament" / f"{src_name}.json").exists()
        assert (root / "filament" / f"{src_name}.info").exists()

    def test_duplicate_targets_in_batch_only_first_renamed(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA A", {"compatible_printers": [MACHINE_A]})
        write_profile(root, "filament", "PLA B", {"compatible_printers": [MACHINE_A]})
        profiles = load_tree(profile_tree)
        pa = get_profile(profiles, ProfileCategory.FILAMENT, "PLA A")
        pb = get_profile(profiles, ProfileCategory.FILAMENT, "PLA B")

        renamed = execute_renames(
            console,
            [RenameAction(pa, "PLA A", "PLA C"), RenameAction(pb, "PLA B", "PLA C")],
            tmp_path / "_backup", all_profiles=profiles,
        )
        assert renamed == 1
        assert (root / "filament" / "PLA C.json").exists()
        # Second profile untouched, both files intact
        assert (root / "filament" / "PLA B.json").exists()
        assert (root / "filament" / "PLA B.info").exists()

    def test_process_rename_broadens_to_all_model_nozzle_machines(self, profile_tree, console, tmp_path):
        root = profile_tree / "1234567890"
        write_profile(
            root, "process", "0.2mm - Draft (Doomcube - 0.4mm)",
            {"compatible_printers": [MACHINE_A]},
        )
        profiles = load_tree(profile_tree)
        proc = get_profile(profiles, ProfileCategory.PROCESS, "0.2mm - Draft (Doomcube - 0.4mm)")

        execute_renames(
            console,
            [RenameAction(proc, "0.2mm - Draft (Doomcube - 0.4mm)", "0.20mm - Draft (Doomcube - 0.4mm)")],
            tmp_path / "_backup", all_profiles=profiles,
        )
        cp = read_json(root, "process", "0.20mm - Draft (Doomcube - 0.4mm)")["compatible_printers"]
        assert sorted(cp) == sorted([MACHINE_A, MACHINE_B])


# ---------------------------------------------------------------------------
# Printer removal
# ---------------------------------------------------------------------------


class TestPrinterRemoval:
    def test_removal_archives_exclusive_and_strips_shared(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        machine = get_profile(profiles, ProfileCategory.MACHINE, MACHINE_A)
        exclusive, shared = find_printer_dependents(profiles, MACHINE_A)

        assert {p.name for p in exclusive} == {"ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)"}
        assert {p.name for p in shared} == {"PLA - Shared", "0.20mm - Standard (Doomcube - 0.4mm)"}

        execute_printer_removal(console, machine, exclusive, shared, tmp_path / "_backup")

        root = profile_tree / "1234567890"
        assert not (root / "machine" / f"{MACHINE_A}.json").exists()
        assert not (root / "filament" / "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm).json").exists()
        assert read_json(root, "filament", "PLA - Shared")["compatible_printers"] == [MACHINE_B]

    def test_duplicate_cp_entries_treated_as_exclusive(self, profile_tree):
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "Dupe CP", {"compatible_printers": [MACHINE_A, MACHINE_A]})
        profiles = load_tree(profile_tree)
        exclusive, shared = find_printer_dependents(profiles, MACHINE_A)
        assert "Dupe CP" in {p.name for p in exclusive}
        assert "Dupe CP" not in {p.name for p in shared}

    def test_shared_profile_never_left_with_empty_cp(self, profile_tree, console, tmp_path):
        # A profile whose OTHER printer reference is stripped case-differently
        # can't happen via find_printer_dependents, but execute must still
        # archive rather than write [] if stripping empties the list.
        profiles = load_tree(profile_tree)
        machine = get_profile(profiles, ProfileCategory.MACHINE, MACHINE_A)
        only_a = get_profile(profiles, ProfileCategory.FILAMENT, "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)")

        # Force it through the "shared" path even though stripping empties it
        execute_printer_removal(console, machine, [], [only_a], tmp_path / "_backup")

        root = profile_tree / "1234567890"
        json_path = root / "filament" / "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm).json"
        assert not json_path.exists()  # archived, not saved with []


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestBackup:
    def _run_backup(self, profile_tree, *args):
        runner = CliRunner()
        return runner.invoke(
            cli,
            ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", "backup", *args],
        )

    def test_backup_snapshots_whole_library_with_manifest(self, profile_tree):
        result = self._run_backup(profile_tree)
        assert result.exit_code == 0, result.output

        backup_dir = next((profile_tree.parent / "_backup").iterdir())
        # Every profile pair in the fixture is present in the snapshot.
        for category, name in [
            ("machine", MACHINE_A),
            ("filament", "PLA - Shared"),
            ("process", "0.20mm - Standard (Doomcube - 0.4mm)"),
        ]:
            assert (backup_dir / category / f"{name}.json").exists()
            assert (backup_dir / category / f"{name}.info").exists()

        # Provenance + restore path both work off the manifest.
        from orcaslicer_cleaner.fileops import load_operation
        assert load_operation(backup_dir) == "backup"
        manifest = load_manifest(backup_dir)
        assert manifest["filament/PLA - Shared.json"] == str(
            profile_tree / "1234567890" / "filament" / "PLA - Shared.json"
        )

    def test_backup_leaves_originals_in_place(self, profile_tree):
        before = {p for p in (profile_tree / "1234567890").rglob("*") if p.is_file()}
        result = self._run_backup(profile_tree)
        assert result.exit_code == 0, result.output
        after = {p for p in (profile_tree / "1234567890").rglob("*") if p.is_file()}
        assert before == after  # copy-only: nothing moved or deleted

    def test_backup_round_trips_through_restore(self, profile_tree):
        assert self._run_backup(profile_tree).exit_code == 0
        target_json = profile_tree / "1234567890" / "filament" / "PLA - Shared.json"
        target_json.unlink()

        backup_dir = next((profile_tree.parent / "_backup").iterdir())
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent",
             "restore", backup_dir.name, "--force"],
        )
        assert result.exit_code == 0, result.output
        assert target_json.exists()


class TestRestore:
    def _archive(self, profile_tree, console, name="PLA - Shared"):
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, name)
        backup_root = profile_tree.parent / "OrcaBackup"
        execute_actions(console, [CleanAction("archive", target, "test")], backup_root)
        return target, next(backup_root.iterdir())

    def _run_restore(self, profile_tree, args):
        runner = CliRunner()
        return runner.invoke(
            cli,
            ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", "restore", *args],
        )

    def test_restore_uses_manifest_to_restore_original_location(self, profile_tree, console):
        # Add a second user root that sorts FIRST — without the manifest,
        # restore would dump files there instead of the original account dir.
        (profile_tree / "0000000000" / "filament").mkdir(parents=True)

        target, backup_dir = self._archive(profile_tree, console)
        # Point the CLI's expected backup root at our backup
        backup_dir_root = profile_tree.parent / "_backup"
        backup_dir_root.mkdir(exist_ok=True)
        (backup_dir_root / backup_dir.name).symlink_to(backup_dir)

        result = self._run_restore(profile_tree, [backup_dir.name, "--force"])
        assert result.exit_code == 0, result.output

        assert target.json_path.exists()
        assert target.info_path.exists()
        assert not (profile_tree / "0000000000" / "filament" / "PLA - Shared.json").exists()

    def test_restore_backs_up_overwritten_files(self, profile_tree, console):
        _, backup_dir = self._archive(profile_tree, console)
        backup_dir_root = profile_tree.parent / "_backup"
        backup_dir_root.mkdir(exist_ok=True)
        (backup_dir_root / backup_dir.name).symlink_to(backup_dir)

        # Recreate the profile with DIFFERENT content, then restore over it
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Shared", {"compatible_printers": ["Changed"]})

        result = self._run_restore(profile_tree, [backup_dir.name, "--force"])
        assert result.exit_code == 0, result.output

        # Restored content is the original
        assert read_json(root, "filament", "PLA - Shared")["compatible_printers"] == [MACHINE_A, MACHINE_B]
        # The overwritten "Changed" version was backed up somewhere under _backup
        overwrite_dirs = [d for d in backup_dir_root.iterdir() if d.name != backup_dir.name]
        assert overwrite_dirs, "expected a backup dir for overwritten files"
        saved = json.loads(
            (overwrite_dirs[0] / "filament" / "PLA - Shared.json").read_text()
        )
        assert saved["compatible_printers"] == ["Changed"]

    def test_restore_after_machine_rename_removes_new_name_pair(self, profile_tree, console):
        """Restoring a rename backup must remove the new-name files, not just
        copy the old-name pair back — otherwise both machines exist."""
        profiles = load_tree(profile_tree)
        machine = get_profile(profiles, ProfileCategory.MACHINE, MACHINE_A)
        new_name = "Doomcube - LGX Lite Pro - TeaKettle - 0.6mm"
        backup_root = profile_tree.parent / "_backup"

        execute_renames(
            console, [RenameAction(machine, MACHINE_A, new_name)],
            backup_root, all_profiles=profiles,
        )
        backup_dir = next(backup_root.iterdir())
        root = profile_tree / "1234567890"
        assert (root / "machine" / f"{new_name}.json").exists()

        result = self._run_restore(profile_tree, [backup_dir.name, "--force"])
        assert result.exit_code == 0, result.output

        # Old pair is back, new pair is gone — exact pre-rename state
        assert (root / "machine" / f"{MACHINE_A}.json").exists()
        assert (root / "machine" / f"{MACHINE_A}.info").exists()
        assert not (root / "machine" / f"{new_name}.json").exists()
        assert not (root / "machine" / f"{new_name}.info").exists()

        # Cascaded filament reference reverted too
        cp = read_json(root, "filament", "PLA - Shared")["compatible_printers"]
        assert MACHINE_A in cp and new_name not in cp

        # The removed new-name files were backed up, so the restore is undoable
        overwrite_dirs = [d for d in backup_root.iterdir() if d.name != backup_dir.name]
        assert overwrite_dirs
        assert (overwrite_dirs[0] / "machine" / f"{new_name}.json").exists()

    def test_restore_single_profile_of_renamed_machine_keeps_new_pair(self, profile_tree, console):
        """A --profile restore of a renamed machine must NOT delete the
        new-name pair: the cascaded dependents are excluded by the filter and
        still reference the new name — removing it would break them."""
        profiles = load_tree(profile_tree)
        machine = get_profile(profiles, ProfileCategory.MACHINE, MACHINE_A)
        new_name = "Doomcube - LGX Lite Pro - TeaKettle - 0.6mm"
        backup_root = profile_tree.parent / "_backup"

        execute_renames(
            console, [RenameAction(machine, MACHINE_A, new_name)],
            backup_root, all_profiles=profiles,
        )
        backup_dir = next(backup_root.iterdir())

        result = self._run_restore(
            profile_tree, [backup_dir.name, "--profile", MACHINE_A, "--force"]
        )
        assert result.exit_code == 0, result.output

        root = profile_tree / "1234567890"
        # Old machine restored, new machine STILL present (not deleted)
        assert (root / "machine" / f"{MACHINE_A}.json").exists()
        assert (root / "machine" / f"{new_name}.json").exists()
        # Dependents still point at the new name, which still exists
        cp = read_json(root, "filament", "PLA - Shared")["compatible_printers"]
        assert new_name in cp
        assert "Restore the full backup" in " ".join(result.output.split())

    def test_restore_falls_back_when_manifest_root_is_gone(self, profile_tree, console):
        """A manifest pointing at a user root that no longer exists must not
        silently recreate that dead tree — restore to the current root."""
        target, backup_dir = self._archive(profile_tree, console)
        backup_dir_root = profile_tree.parent / "_backup"
        backup_dir_root.mkdir(exist_ok=True)
        (backup_dir_root / backup_dir.name).symlink_to(backup_dir)

        # Rewrite the manifest to point at a vanished account dir
        manifest_path = backup_dir / "manifest.json"
        data = json.loads(manifest_path.read_text())
        gone_root = profile_tree / "9999999999"
        data["files"] = {
            rel: str(gone_root / "filament" / Path(orig).name)
            for rel, orig in data["files"].items()
        }
        manifest_path.write_text(json.dumps(data))

        result = self._run_restore(profile_tree, [backup_dir.name, "--force"])
        assert result.exit_code == 0, result.output

        assert not gone_root.exists()
        assert target.json_path.exists()  # restored into the real root

    def test_restore_prefers_earliest_duplicate_copy(self, profile_tree, console):
        """When one backup dir holds Name.json and Name_1.json for the same
        original, restore must apply the earliest (true original), not the
        later intermediate state."""
        _, backup_dir = self._archive(profile_tree, console)  # original content

        # Recreate with intermediate content and archive into the SAME dir
        root = profile_tree / "1234567890"
        write_profile(root, "filament", "PLA - Shared", {"compatible_printers": ["Intermediate"]})
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        from orcaslicer_cleaner.cleaner import _archive_profile
        _archive_profile(target, backup_dir)

        backup_dir_root = profile_tree.parent / "_backup"
        backup_dir_root.mkdir(exist_ok=True)
        (backup_dir_root / backup_dir.name).symlink_to(backup_dir)

        result = self._run_restore(profile_tree, [backup_dir.name, "--force"])
        assert result.exit_code == 0, result.output

        cp = read_json(root, "filament", "PLA - Shared")["compatible_printers"]
        assert cp == [MACHINE_A, MACHINE_B]  # the earliest version, not "Intermediate"

    def test_restore_single_profile_filter(self, profile_tree, console):
        profiles = load_tree(profile_tree)
        t1 = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        t2 = get_profile(profiles, ProfileCategory.FILAMENT, "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)")
        backup_root = profile_tree.parent / "_backup"
        execute_actions(
            console,
            [CleanAction("archive", t1, "test"), CleanAction("archive", t2, "test")],
            backup_root,
        )
        backup_dir = next(backup_root.iterdir())

        result = self._run_restore(
            profile_tree, [backup_dir.name, "--profile", "PLA - Shared", "--force"]
        )
        assert result.exit_code == 0, result.output
        assert t1.json_path.exists()
        assert not t2.json_path.exists()


class TestBackupMirroring:
    """A user-supplied --backup-dir is an ADDITIONAL copy, never a replacement:
    the canonical backup always lands in the default _backup so restore/undo
    keep working regardless of flags."""

    def test_execute_actions_mirrors_to_extra_root(self, profile_tree, console, tmp_path):
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        primary_root = tmp_path / "_backup"
        mirror_root = tmp_path / "external"

        actions = [CleanAction(action="archive", profile=target, reason="test")]
        assert execute_actions(console, actions, primary_root, mirror_root=mirror_root) == 1

        primary = next(primary_root.iterdir())
        mirror = next(mirror_root.iterdir())
        # Same timestamped name, and the mirror is self-contained (files + manifest)
        assert mirror.name == primary.name
        assert (mirror / "filament" / "PLA - Shared.json").exists()
        assert (mirror / "filament" / "PLA - Shared.info").exists()
        assert load_manifest(mirror) == load_manifest(primary)

    def test_resolve_backup_roots_collapses_identical_dir(self, tmp_path):
        from orcaslicer_cleaner.cli import _resolve_backup_roots

        profile_dir = tmp_path / "user"
        default_root = tmp_path / "_backup"

        # No custom dir -> no mirror
        primary, mirror = _resolve_backup_roots(profile_dir, None)
        assert primary == default_root and mirror is None

        # Custom dir pointing at the default -> no redundant mirror
        primary, mirror = _resolve_backup_roots(profile_dir, default_root)
        assert primary == default_root and mirror is None

        # A genuinely different custom dir -> becomes the mirror
        custom = tmp_path / "external"
        primary, mirror = _resolve_backup_roots(profile_dir, custom)
        assert primary == default_root and mirror == custom

    def test_mirror_failure_does_not_abort_mutation(self, profile_tree, console, tmp_path):
        """A broken mirror destination must not prevent the real backup/mutation."""
        profiles = load_tree(profile_tree)
        target = get_profile(profiles, ProfileCategory.FILAMENT, "PLA - Shared")
        primary_root = tmp_path / "_backup"
        # A file (not a dir) where the mirror root should be -> mkdir/copy fails
        bad_mirror = tmp_path / "blocked"
        bad_mirror.write_text("not a directory")

        actions = [CleanAction(action="archive", profile=target, reason="test")]
        assert execute_actions(console, actions, primary_root, mirror_root=bad_mirror) == 1
        assert not target.json_path.exists()  # mutation still happened
        assert (next(primary_root.iterdir()) / "filament" / "PLA - Shared.json").exists()
