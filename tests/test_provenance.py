"""Tests for backup provenance (operation labels) and the undo command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from orcaslicer_cleaner import loader
from orcaslicer_cleaner.cleaner import CleanAction, execute_actions
from orcaslicer_cleaner.cli import cli
from orcaslicer_cleaner.fileops import backup_copy, create_backup_dir, load_operation
from orcaslicer_cleaner.models import ProfileCategory

MACHINE = "Doomcube - WWBMG - TeaKettle - 0.4mm"


def write_profile(root: Path, category: str, name: str, settings: dict) -> None:
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"{name}.json").write_text(json.dumps(settings) + "\n", encoding="utf-8")
    (cat_dir / f"{name}.info").write_text("updated_time = 1700000000\n", encoding="utf-8")


@pytest.fixture
def profile_tree(tmp_path: Path) -> Path:
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"
    write_profile(root, "machine", MACHINE, {"name": MACHINE})
    write_profile(root, "filament", "PLA - Test", {"compatible_printers": [MACHINE]})
    return user_dir


class TestProvenance:
    def test_operation_recorded_and_loadable(self, tmp_path):
        d = create_backup_dir(tmp_path / "_backup", "clean")
        assert load_operation(d) == "clean"

    def test_no_operation_returns_none(self, tmp_path):
        d = create_backup_dir(tmp_path / "_backup")
        assert load_operation(d) is None

    def test_backup_copy_preserves_operation(self, tmp_path):
        d = create_backup_dir(tmp_path / "_backup", "fix-links")
        src = tmp_path / "some.json"
        src.write_text("{}")
        backup_copy(src, d, "filament")
        assert load_operation(d) == "fix-links"
        manifest = json.loads((d / "manifest.json").read_text())
        assert "filament/some.json" in manifest["files"]

    def test_mutation_backups_carry_labels(self, profile_tree, tmp_path):
        merged = {c: [] for c in ProfileCategory}
        for root in loader.discover_profile_dirs(profile_tree):
            for c, ps in loader.load_profiles(root).items():
                merged[c].extend(ps)
        target = next(p for p in merged[ProfileCategory.FILAMENT] if p.name == "PLA - Test")
        console = Console(file=open("/dev/null", "w"))
        backup_root = tmp_path / "_backup"
        execute_actions(console, [CleanAction("archive", target, "test")], backup_root)
        d = next(backup_root.iterdir())
        assert load_operation(d) == "clean"


class TestUndo:
    def test_undo_restores_most_recent_backup(self, profile_tree):
        merged = {c: [] for c in ProfileCategory}
        for root in loader.discover_profile_dirs(profile_tree):
            for c, ps in loader.load_profiles(root).items():
                merged[c].extend(ps)
        target = next(p for p in merged[ProfileCategory.FILAMENT] if p.name == "PLA - Test")
        console = Console(file=open("/dev/null", "w"))
        backup_root = profile_tree.parent / "_backup"
        execute_actions(console, [CleanAction("archive", target, "test")], backup_root)
        assert not target.json_path.exists()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", "undo", "--force"],
        )
        assert result.exit_code == 0, result.output
        assert "clean" in result.output  # operation label shown
        assert target.json_path.exists()
        assert target.info_path.exists()

    def test_undo_with_no_backups(self, profile_tree):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", "undo", "--force"],
        )
        assert "No backup" in result.output or "No backups" in result.output
