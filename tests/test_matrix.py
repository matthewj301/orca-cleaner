"""Tests for the read-only `ocs matrix` inventory command.

Builds a small tmp_path profile tree and asserts on the rendered Rich
output (via Console(record=True).export_text()) rather than internal data
structures, since matrix.py is pure reporting.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from rich.console import Console

from orcaslicer_cleaner import loader
from orcaslicer_cleaner.cli import cli
from orcaslicer_cleaner.matrix import print_filament_matrix, print_process_matrix
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


def make_console() -> Console:
    return Console(record=True, force_terminal=False, width=200)


def build_profile_tree(tmp_path: Path) -> Path:
    """user/<account>/{machine,filament,process}/ with a mix of coverage,
    redundancy, empty compatible_printers, and an unknown printer ref."""
    user_dir = tmp_path / "user"
    root = user_dir / "1234567890"

    for machine in (MACHINE_A, MACHINE_B, MACHINE_C):
        write_profile(root, "machine", machine, {"name": machine, "printer_settings_id": machine})

    # Two filament profiles for the same material/brand on machine A (redundancy).
    write_profile(
        root, "filament", "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)",
        {"compatible_printers": [MACHINE_A]},
    )
    write_profile(
        root, "filament", "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm) v2",
        {"compatible_printers": [MACHINE_A]},
    )
    # Shared across two machines.
    write_profile(
        root, "filament", "PLA - Filamentum (Doomcube)",
        {"compatible_printers": [MACHINE_A, MACHINE_B]},
    )
    # Empty compatible_printers -- visible to ALL printers, should be flagged.
    write_profile(
        root, "filament", "PETG - Overture (Doomcube)",
        {"compatible_printers": []},
    )
    # References an unknown/broken machine name.
    write_profile(
        root, "filament", "TPU - SainSmart (Ghost Printer)",
        {"compatible_printers": ["Ghost Printer"]},
    )

    # Process profiles: model-scoped columns.
    write_profile(
        root, "process", "0.20mm - Standard (Doomcube - 0.4mm)",
        {"compatible_printers": [MACHINE_A, MACHINE_B]},
    )
    write_profile(
        root, "process", "0.12mm - Fine (Positron - 0.6mm)",
        {"compatible_printers": [MACHINE_C]},
    )
    write_profile(
        root, "process", "0.28mm - Draft (Doomcube - 0.4mm)",
        {"compatible_printers": []},
    )

    return user_dir


# ---------------------------------------------------------------------------
# Filament matrix
# ---------------------------------------------------------------------------


class TestFilamentMatrix:
    def test_counts_and_empty_and_unknown(self, tmp_path):
        user_dir = build_profile_tree(tmp_path)
        profiles = load_tree(user_dir)

        console = make_console()
        print_filament_matrix(console, profiles)
        output = console.export_text()

        # Legend maps M1..Mn to full machine names.
        assert "Machine Legend" in output
        assert MACHINE_A in output
        assert MACHINE_B in output
        assert MACHINE_C in output

        # Row for "asa - 3do" should show 2 profiles under machine A's column.
        row_lines = [line for line in output.splitlines() if "asa - 3do" in line]
        assert row_lines, output
        assert "2" in row_lines[0]

        # Empty compatible_printers row shows up under ALL column.
        petg_lines = [line for line in output.splitlines() if "petg - overture" in line]
        assert petg_lines, output
        assert "1" in petg_lines[0]

        # Unknown printer ref lands under "?" column.
        tpu_lines = [line for line in output.splitlines() if "tpu - sainsmart" in line]
        assert tpu_lines, output

        # Summary line present.
        assert "profile(s)" in output
        assert "empty compatible_printers" in output

    def test_shared_profile_counts_once_per_machine(self, tmp_path):
        user_dir = build_profile_tree(tmp_path)
        profiles = load_tree(user_dir)

        console = make_console()
        print_filament_matrix(console, profiles)
        output = console.export_text()

        pla_lines = [line for line in output.splitlines() if "pla - filamentum" in line]
        assert pla_lines, output
        # Should have a "1" under machine A's and machine B's columns (not the
        # count of machine C or the ALL column), i.e. it's counted per-machine.
        assert pla_lines[0].count("1") >= 2


# ---------------------------------------------------------------------------
# Process matrix
# ---------------------------------------------------------------------------


class TestProcessMatrix:
    def test_rows_group_by_model_columns(self, tmp_path):
        user_dir = build_profile_tree(tmp_path)
        profiles = load_tree(user_dir)

        console = make_console()
        print_process_matrix(console, profiles)
        output = console.export_text()

        # Columns are deduped printer MODELS, not full machine names.
        assert "Printer Model Legend" in output
        assert "Doomcube" in output
        assert "Positron" in output
        # Full machine names (with extruder/hotend/nozzle) should not appear
        # as columns in the process legend.
        assert MACHINE_A not in output
        assert MACHINE_B not in output

        standard_lines = [line for line in output.splitlines() if "0.20mm - standard" in line]
        assert standard_lines, output

        draft_lines = [line for line in output.splitlines() if "0.28mm - draft" in line]
        assert draft_lines, output
        assert "1" in draft_lines[0]  # empty compatible_printers -> ALL column

        assert "profile(s)" in output
        assert "empty compatible_printers" in output


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestMatrixCli:
    def _run(self, user_dir: Path, args: list[str]):
        runner = CliRunner()
        return runner.invoke(
            cli,
            ["--profile-dir", str(user_dir), "--system-profiles", "/nonexistent", "matrix", *args],
        )

    def test_matrix_default_prints_both(self, tmp_path):
        user_dir = build_profile_tree(tmp_path)
        result = self._run(user_dir, [])
        assert result.exit_code == 0, result.output
        assert "Filament Coverage Matrix" in result.output
        assert "Process Coverage Matrix" in result.output

    def test_matrix_category_filament_only(self, tmp_path):
        user_dir = build_profile_tree(tmp_path)
        result = self._run(user_dir, ["--category", "filament"])
        assert result.exit_code == 0, result.output
        assert "Filament Coverage Matrix" in result.output
        assert "Process Coverage Matrix" not in result.output

    def test_matrix_category_process_only(self, tmp_path):
        user_dir = build_profile_tree(tmp_path)
        result = self._run(user_dir, ["--category", "process"])
        assert result.exit_code == 0, result.output
        assert "Process Coverage Matrix" in result.output
        assert "Filament Coverage Matrix" not in result.output
