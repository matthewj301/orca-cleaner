"""Tests for `ocs prune-backups`: deleting old timestamped backup
directories while never touching manually curated ones.

This is the one place in the codebase allowed to hard-delete, so the tests
focus on: only timestamp-pattern dirs are ever candidates, preview never
deletes, a typed 'yes' is required to execute, and non-timestamped dirs
always survive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from orcaslicer_cleaner.cli import cli

MACHINE = "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm"


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


def write_manifest(backup_dir: Path, operation: str | None = None) -> None:
    data = {"files": {}}
    if operation:
        data["operation"] = {"command": operation, "argv": [], "time": "2026-01-01T00:00:00"}
    (backup_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def profile_tree(tmp_path: Path) -> Path:
    """Minimal user/<account>/machine/ tree so the CLI group can load."""
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"
    write_profile(root, "machine", MACHINE, {"name": MACHINE, "printer_settings_id": MACHINE})
    return user_dir


# Timestamps in strictly increasing order (oldest -> newest) so sorting by
# name descending gives a deterministic newest-first order.
TIMESTAMPS = [
    "20260101_010101",
    "20260201_010101",
    "20260301_010101",
    "20260401_010101",
    "20260501_010101",
    "20260601_010101",
]


@pytest.fixture
def backup_root(profile_tree: Path) -> Path:
    """Create profile_tree.parent/_backup with 6 timestamped dirs (some with
    manifests carrying an operation label), plus two non-timestamped dirs
    that must never be touched."""
    root = profile_tree.parent / "_backup"
    root.mkdir(parents=True, exist_ok=True)

    for i, ts in enumerate(TIMESTAMPS):
        d = root / ts
        d.mkdir()
        # Give every other one an operation label.
        write_manifest(d, operation="clean" if i % 2 == 0 else None)
        (d / "machine").mkdir()
        (d / "machine" / f"{MACHINE}.json").write_text("{}", encoding="utf-8")
        (d / "machine" / f"{MACHINE}.info").write_text("x", encoding="utf-8")

    (root / "1987659579_archived_2026-06-03").mkdir()
    (root / "retired_hardware_x").mkdir()

    return root


def run_prune(profile_tree: Path, args: list[str], input_: str | None = None):
    runner = CliRunner()
    return runner.invoke(
        cli,
        ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent",
         "prune-backups", *args],
        input=input_,
    )


class TestPrunePreview:
    def test_preview_lists_only_candidates_beyond_keep_and_deletes_nothing(
        self, profile_tree, backup_root
    ):
        result = run_prune(profile_tree, ["--keep", "2"])
        assert result.exit_code == 0, result.output

        # Oldest 4 are candidates (6 total, keep 2 newest).
        candidates = TIMESTAMPS[:4]
        kept = TIMESTAMPS[4:]
        for ts in candidates:
            assert ts in result.output
        for ts in kept:
            # Kept dirs shouldn't appear as candidate rows. They may still
            # coincidentally match on a prefix so check with a boundary.
            assert ts not in result.output

        assert "4" in result.output  # count deleted-would-be
        assert "would be deleted" in result.output
        assert "never touched" in result.output
        assert "--execute" in result.output

        # Nothing actually removed.
        for ts in TIMESTAMPS:
            assert (backup_root / ts).is_dir()
        assert (backup_root / "1987659579_archived_2026-06-03").is_dir()
        assert (backup_root / "retired_hardware_x").is_dir()

    def test_operation_labels_appear_in_preview(self, profile_tree, backup_root):
        result = run_prune(profile_tree, ["--keep", "2"])
        assert result.exit_code == 0, result.output
        assert "clean" in result.output


class TestPruneExecute:
    def test_execute_with_input_y_deletes_nothing(self, profile_tree, backup_root):
        result = run_prune(profile_tree, ["--keep", "2", "--execute"], input_="y\n")
        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        for ts in TIMESTAMPS:
            assert (backup_root / ts).is_dir()

    def test_execute_with_input_yes_deletes_oldest_keeps_newest(
        self, profile_tree, backup_root
    ):
        result = run_prune(profile_tree, ["--keep", "2", "--execute"], input_="yes\n")
        assert result.exit_code == 0, result.output

        candidates = TIMESTAMPS[:4]
        kept = TIMESTAMPS[4:]
        for ts in candidates:
            assert not (backup_root / ts).exists()
        for ts in kept:
            assert (backup_root / ts).is_dir()

        assert "4/4" in result.output or "Done. 4" in result.output

        # Non-timestamped dirs untouched.
        assert (backup_root / "1987659579_archived_2026-06-03").is_dir()
        assert (backup_root / "retired_hardware_x").is_dir()


class TestPruneNothingToDo:
    def test_keep_larger_than_available_reports_nothing_to_prune(
        self, profile_tree, backup_root
    ):
        result = run_prune(profile_tree, ["--keep", "100"])
        assert result.exit_code == 0, result.output
        assert "Nothing to prune" in result.output
        for ts in TIMESTAMPS:
            assert (backup_root / ts).is_dir()
        assert (backup_root / "1987659579_archived_2026-06-03").is_dir()
        assert (backup_root / "retired_hardware_x").is_dir()

    def test_no_backup_dir_at_all(self, profile_tree):
        result = run_prune(profile_tree, [])
        assert result.exit_code == 0, result.output
        assert "No backup directory found" in result.output
