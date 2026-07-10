"""Integration tests for the safety rails wired into the CLI: escalated
confirmation proportional to blast radius, and the post-operation
coverage/breakage report.

This is a regression suite for the 2026-07 data-loss incident where
`clean --execute` archived ~40 profiles on a single y/n confirm and nobody
noticed the coverage loss until later. These tests only exercise the CLI
(via CliRunner) against a tmp_path profile tree — never the user's real
profiles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from orcaslicer_cleaner.cli import cli

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


# A timestamp far enough in the past to be "stale" under the default
# 365-day threshold, used to trigger `clean --type stale` on real,
# parseable profiles (so their compatible_printers links are real coverage
# that can actually be lost).
STALE_TIME = 1
FRESH_TIME = 1_900_000_000


def run_cli(profile_tree: Path, args: list[str], input: str | None = None):
    runner = CliRunner()
    return runner.invoke(
        cli,
        ["--profile-dir", str(profile_tree), "--system-profiles", "/nonexistent", *args],
        input=input,
    )


@pytest.fixture
def big_plan_tree(tmp_path: Path) -> Path:
    """A tree where `clean --type stale` archives >=3 profiles and more
    than 15% of the filament category: 3 stale out of 4 filaments (75%),
    all exclusively linked to MACHINE_A so the printer loses filament
    coverage entirely (the 4th filament is fresh and only linked to
    MACHINE_B, so it survives but doesn't save MACHINE_A's coverage)."""
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"
    write_profile(root, "machine", MACHINE_A, {"name": MACHINE_A, "printer_settings_id": MACHINE_A}, updated_time=FRESH_TIME)
    write_profile(root, "machine", MACHINE_B, {"name": MACHINE_B, "printer_settings_id": MACHINE_B}, updated_time=FRESH_TIME)

    write_profile(
        root, "filament", "PLA - Stale One",
        {"compatible_printers": [MACHINE_A]}, updated_time=STALE_TIME,
    )
    write_profile(
        root, "filament", "PETG - Stale Two",
        {"compatible_printers": [MACHINE_A]}, updated_time=STALE_TIME,
    )
    write_profile(
        root, "filament", "ABS - Stale Three",
        {"compatible_printers": [MACHINE_A]}, updated_time=STALE_TIME,
    )
    write_profile(
        root, "filament", "PLA - Fine",
        {"compatible_printers": [MACHINE_B]}, updated_time=FRESH_TIME,
    )
    return user_dir


@pytest.fixture
def small_plan_tree(tmp_path: Path) -> Path:
    """A tree where `clean --type stale` archives exactly one profile that
    isn't linked to any printer (empty compatible_printers, i.e. "visible
    to ALL printers" — excluded from coverage-loss reporting by design):
    a routine, low-blast-radius operation with nothing to lose."""
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"
    write_profile(root, "machine", MACHINE_A, {"name": MACHINE_A, "printer_settings_id": MACHINE_A}, updated_time=FRESH_TIME)
    write_profile(root, "machine", MACHINE_B, {"name": MACHINE_B, "printer_settings_id": MACHINE_B}, updated_time=FRESH_TIME)

    write_profile(
        root, "filament", "PLA - Stale One",
        {"compatible_printers": []}, updated_time=STALE_TIME,
    )
    # Plenty of healthy coverage for both printers so archiving the one
    # stale profile costs nothing.
    for i in range(10):
        write_profile(
            root, "filament", f"PLA - Fine {i}",
            {"compatible_printers": [MACHINE_A, MACHINE_B]}, updated_time=FRESH_TIME,
        )
    write_profile(
        root, "process", "0.20mm - Standard (Doomcube - 0.4mm)",
        {"compatible_printers": [MACHINE_A, MACHINE_B]}, updated_time=FRESH_TIME,
    )
    return user_dir


class TestCleanBlastRadius:
    def test_large_plan_plain_y_does_not_execute(self, big_plan_tree):
        result = run_cli(
            big_plan_tree, ["clean", "--type", "stale", "--execute"], input="y\n"
        )
        assert result.exit_code == 0, result.output
        root = big_plan_tree / "1234567890"
        # A plain "y" must not satisfy the hard-confirm gate: files remain.
        assert (root / "filament" / "PLA - Stale One.json").exists()
        assert (root / "filament" / "PETG - Stale Two.json").exists()
        assert (root / "filament" / "ABS - Stale Three.json").exists()

    def test_large_plan_typed_yes_executes(self, big_plan_tree):
        result = run_cli(
            big_plan_tree, ["clean", "--type", "stale", "--execute"], input="yes\n"
        )
        assert result.exit_code == 0, result.output
        root = big_plan_tree / "1234567890"
        assert not (root / "filament" / "PLA - Stale One.json").exists()
        assert not (root / "filament" / "PETG - Stale Two.json").exists()
        assert not (root / "filament" / "ABS - Stale Three.json").exists()
        assert "Blast Radius Warning" in result.output

    def test_small_plan_plain_y_executes(self, small_plan_tree):
        result = run_cli(
            small_plan_tree, ["clean", "--type", "stale", "--execute"], input="y\n"
        )
        assert result.exit_code == 0, result.output
        root = small_plan_tree / "1234567890"
        assert not (root / "filament" / "PLA - Stale One.json").exists()

    def test_large_plan_reports_lost_coverage_and_undo_hint(self, big_plan_tree):
        result = run_cli(
            big_plan_tree, ["clean", "--type", "stale", "--execute"], input="yes\n"
        )
        assert result.exit_code == 0, result.output
        assert MACHINE_A in result.output
        assert "lost" in result.output.lower()
        assert "ocs undo" in result.output

    def test_small_plan_reports_no_coverage_lost(self, small_plan_tree):
        result = run_cli(
            small_plan_tree, ["clean", "--type", "stale", "--execute"], input="y\n"
        )
        assert result.exit_code == 0, result.output
        assert "no coverage lost" in result.output.lower()


class TestRemovePrinterAlwaysHardConfirms:
    @pytest.fixture
    def printer_tree(self, tmp_path: Path) -> Path:
        user_dir = tmp_path / "user"
        root = user_dir / "1234567890"
        write_profile(root, "machine", MACHINE_A, {"name": MACHINE_A, "printer_settings_id": MACHINE_A})
        write_profile(root, "machine", MACHINE_B, {"name": MACHINE_B, "printer_settings_id": MACHINE_B})
        write_profile(
            root, "filament", "ASA - Only A",
            {"compatible_printers": [MACHINE_A]},
        )
        write_profile(
            root, "filament", "PLA - Shared",
            {"compatible_printers": [MACHINE_A, MACHINE_B]},
        )
        return user_dir

    def _select_machine_a(self, profiles_dir: Path) -> str:
        # Machines are listed sorted by name; MACHINE_A sorts before MACHINE_B.
        return "1\n"

    def test_plain_y_does_not_execute(self, printer_tree):
        result = run_cli(
            printer_tree, ["remove-printer"], input=self._select_machine_a(printer_tree) + "y\n"
        )
        assert result.exit_code == 0, result.output
        root = printer_tree / "1234567890"
        assert (root / "machine" / f"{MACHINE_A}.json").exists()
        assert (root / "filament" / "ASA - Only A.json").exists()

    def test_typed_yes_executes(self, printer_tree):
        result = run_cli(
            printer_tree, ["remove-printer"], input=self._select_machine_a(printer_tree) + "yes\n"
        )
        assert result.exit_code == 0, result.output
        root = printer_tree / "1234567890"
        assert not (root / "machine" / f"{MACHINE_A}.json").exists()
        assert not (root / "filament" / "ASA - Only A.json").exists()
        assert "Blast Radius Warning" in result.output


class TestRenameNotReportedAsLoss:
    def test_fix_names_rename_reports_no_coverage_lost(self, tmp_path):
        """Regression: a successful rename changes the profile's name, which
        the coverage snapshot keys on — it must NOT show up as a loss."""
        machine = "Doomcube - WWBMG - TeaKettle - 0.4mm"
        user_dir = tmp_path / "user"
        root = user_dir / "1234567890"
        write_profile(root, "machine", machine, {"name": machine})
        write_profile(
            root, "process", "0.2mm - Draft (Doomcube - 0.4mm)",
            {"compatible_printers": [machine]},
        )

        result = run_cli(user_dir, ["fix", "--only", "names"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "Renamed" in result.output
        assert "no coverage lost" in result.output
        assert "lost 1 profile" not in result.output
