"""Tests for interactively assigning printers to profiles with empty
compatible_printers and no hardware hint in their name.

These profiles are invisible to `audit_links` (it needs a hardware hint to
suggest anything) but still show up "visible to ALL printers" in OrcaSlicer,
which is the same functional problem the rest of `fix --only links` solves.
This suite covers `find_unassigned` (detection + suggestion) directly, plus
an end-to-end `ocs fix --only links` CliRunner pass exercising assignment,
archival, and skip.

Local fixtures/helpers only — deliberately not importing from other test
modules per the task brief.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from orcaslicer_cleaner import loader
from orcaslicer_cleaner.cleaner import find_unassigned
from orcaslicer_cleaner.cli import cli
from orcaslicer_cleaner.models import ProfileCategory

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


@pytest.fixture
def base_tree(tmp_path: Path) -> Path:
    """user/<account>/machine/ with two Doomcube variants + one Positron."""
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"
    for machine in (MACHINE_A, MACHINE_B, MACHINE_C):
        write_profile(root, "machine", machine, {"name": machine, "printer_settings_id": machine})
    return user_dir


def run_cli(profile_tree: Path, args: list[str], input: str | None = None):
    runner = CliRunner()
    return runner.invoke(
        cli,
        ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", *args],
        input=input,
    )


# ---------------------------------------------------------------------------
# find_unassigned — detection
# ---------------------------------------------------------------------------


class TestFindUnassignedDetection:
    def test_empty_cp_no_hint_is_unassigned(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "0.20mm - Standard",
            {"compatible_printers": []},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        names = {u.profile.name for u in unassigned}
        assert "0.20mm - Standard" in names

    def test_empty_cp_with_hardware_hint_not_unassigned(self, base_tree):
        root = base_tree / "1234567890"
        # Name ends with a hardware-ish suffix that matches a machine —
        # audit_links' _extract_hardware_hint would already catch this one.
        write_profile(
            root, "filament", "PLA - Positron",
            {"compatible_printers": []},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        names = {u.profile.name for u in unassigned}
        assert "PLA - Positron" not in names

    def test_non_empty_cp_not_unassigned(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "0.20mm - Standard",
            {"compatible_printers": [MACHINE_A]},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        names = {u.profile.name for u in unassigned}
        assert "0.20mm - Standard" not in names

    def test_machine_category_never_included(self, base_tree):
        # Machines are never candidates, regardless of compatible_printers.
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        assert not any(u.profile.category == ProfileCategory.MACHINE for u in unassigned)


# ---------------------------------------------------------------------------
# find_unassigned — suggestions
# ---------------------------------------------------------------------------


class TestFindUnassignedSuggestions:
    def test_suggestion_from_inherits_suggests_all_machines_of_model(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "0.20mm - Standard",
            {"compatible_printers": [], "inherits": "0.20mm Standard @Doomcube 300"},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        item = next(u for u in unassigned if u.profile.name == "0.20mm - Standard")
        # Process profiles are model-scoped: BOTH Doomcube machines suggested.
        assert set(item.suggested_printers) == {MACHINE_A, MACHINE_B}
        assert MACHINE_C not in item.suggested_printers

    def test_suggestion_from_name_prefix_when_no_inherits_match(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "Positron - 0.20mm Fine",
            {"compatible_printers": []},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        item = next(u for u in unassigned if u.profile.name == "Positron - 0.20mm Fine")
        assert MACHINE_C in item.suggested_printers

    def test_no_match_yields_empty_suggestions(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "Voron0.2 - 0.20mm - Speed",
            {"compatible_printers": [], "inherits": "0.20mm Standard @Voron v2 300"},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        item = next(u for u in unassigned if u.profile.name == "Voron0.2 - 0.20mm - Speed")
        # No current machine is a Voron — empty suggestions is fine.
        assert item.suggested_printers == []

    def test_inherits_takes_priority_over_name(self, base_tree):
        root = base_tree / "1234567890"
        # Name mentions Positron, but inherits points at Doomcube — inherits wins.
        write_profile(
            root, "process", "Positron style - Standard",
            {"compatible_printers": [], "inherits": "0.20mm Standard @Doomcube 300"},
        )
        profiles = load_tree(base_tree)
        unassigned = find_unassigned(profiles)
        item = next(u for u in unassigned if u.profile.name == "Positron style - Standard")
        assert set(item.suggested_printers) == {MACHINE_A, MACHINE_B}


# ---------------------------------------------------------------------------
# CliRunner end-to-end: ocs fix --only links
# ---------------------------------------------------------------------------


class TestFixLinksUnassignedCli:
    def test_assign_writes_compatible_printers_with_backup(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "0.20mm - Standard",
            {"compatible_printers": [], "inherits": "0.20mm Standard @Doomcube 300"},
        )
        # Choose machine "1" (sorted: MACHINE_A first), then confirm with plain "y".
        result = run_cli(
            base_tree, ["fix", "--only", "links"], input="1\ny\n"
        )
        assert result.exit_code == 0, result.output

        data = read_json(root, "process", "0.20mm - Standard")
        assert data["compatible_printers"] == [MACHINE_A]

        backup_root = base_tree.parent / "_backup"
        backup_dirs = [d for d in backup_root.iterdir() if d.is_dir()]
        assert backup_dirs, "expected a backup directory to be created"
        backed_up = list((backup_dirs[0] / "process").glob("0.20mm - Standard*.json"))
        assert backed_up, "expected the pre-mutation json to be backed up"
        backed_data = json.loads(backed_up[0].read_text())
        assert backed_data["compatible_printers"] == []

    def test_archive_choice_archives_pair(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "Orphan Process - Standard",
            {"compatible_printers": []},
        )
        result = run_cli(
            base_tree, ["fix", "--only", "links"], input="a\ny\n"
        )
        assert result.exit_code == 0, result.output

        assert not (root / "process" / "Orphan Process - Standard.json").exists()
        assert not (root / "process" / "Orphan Process - Standard.info").exists()

        backup_root = base_tree.parent / "_backup"
        backup_dirs = [d for d in backup_root.iterdir() if d.is_dir()]
        assert backup_dirs
        assert any((d / "process" / "Orphan Process - Standard.json").exists() for d in backup_dirs)

    def test_skip_choice_leaves_untouched(self, base_tree):
        root = base_tree / "1234567890"
        write_profile(
            root, "process", "0.20mm - Standard",
            {"compatible_printers": []},
        )
        # "s" (skip) is also the default, but pass explicitly. No confirm
        # prompt should appear since nothing was queued.
        result = run_cli(
            base_tree, ["fix", "--only", "links"], input="s\n"
        )
        assert result.exit_code == 0, result.output

        data = read_json(root, "process", "0.20mm - Standard")
        assert data["compatible_printers"] == []
        # Untouched means no backup dir was ever created.
        backup_root = base_tree.parent / "_backup"
        assert not backup_root.exists() or not list(backup_root.iterdir())


class TestModelAliases:
    def test_bbl_x1c_inherits_suggests_bambu_machines(self):
        from orcaslicer_cleaner.cleaner import _model_tokens_match

        machines = [
            "Bambu Lab X1 Carbon - Pika - 0.4mm",
            "Doomcube - WWBMG - TeaKettle - 0.4mm",
        ]
        assert _model_tokens_match("0.12mm Fine @BBL X1C", machines) == [
            "Bambu Lab X1 Carbon - Pika - 0.4mm"
        ]

    def test_u1_alias_suggests_snapmaker(self):
        from orcaslicer_cleaner.cleaner import _model_tokens_match

        machines = ["Snapmaker U1 - 0.4mm", "Positron - Sherpa Micro - 0.4mm"]
        assert _model_tokens_match("0.2 Production - U1", machines) == ["Snapmaker U1 - 0.4mm"]
