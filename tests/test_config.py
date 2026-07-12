"""Tests for the TOML configuration loader.

The critical invariant: with no config file, load_config() returns the exact
defaults that reproduce the tool's original hardcoded behavior. Partial configs
override only what they name; typos are rejected loudly rather than ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orcaslicer_cleaner.config import (
    DEFAULT_CONFIG,
    ConfigError,
    load_config,
)


def write_toml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


class TestDefaults:
    def test_no_path_and_no_file_returns_defaults(self, tmp_path, monkeypatch):
        # Point the default path at a location that doesn't exist.
        monkeypatch.setattr(
            "orcaslicer_cleaner.config.DEFAULT_CONFIG_PATH", tmp_path / "nope.toml"
        )
        assert load_config() is DEFAULT_CONFIG

    def test_default_values_match_original_constants(self):
        c = DEFAULT_CONFIG
        assert c.abbreviations == {"TK": "TeaKettle"}
        assert c.hardware_aliases == {"mako": "bambu", "tk": "teakettle"}
        assert c.model_aliases == {
            "bbl": "bambu lab",
            "x1c": "x1 carbon",
            "p1s": "p1s",
            "u1": "snapmaker u1",
        }
        assert c.thresholds.stale_days == 365
        assert c.thresholds.content_similarity == 0.95
        assert c.thresholds.fuzz_material == 90
        assert c.thresholds.fuzz_hardware == 95
        assert c.thresholds.blast_bulk == 20
        assert c.naming.filament.format == "{material} - {brand} ({hardware})"
        assert c.naming.filament.hardware == "{extruder} - {hotend} - {nozzle}"
        assert c.naming.pad_layer_heights is True


class TestLoading:
    def test_explicit_missing_path_errors(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "does-not-exist.toml")

    def test_malformed_toml_errors(self, tmp_path):
        p = write_toml(tmp_path, "this is = = not toml")
        with pytest.raises(ConfigError, match="Malformed TOML"):
            load_config(p)

    def test_partial_threshold_override_keeps_other_defaults(self, tmp_path):
        p = write_toml(tmp_path, "[thresholds]\nstale_days = 90\n")
        c = load_config(p)
        assert c.thresholds.stale_days == 90
        # everything else untouched
        assert c.thresholds.fuzz_material == 90
        assert c.thresholds.content_similarity == 0.95
        # unrelated sections still default
        assert c.abbreviations == DEFAULT_CONFIG.abbreviations

    def test_vocabulary_section_replaces_wholesale(self, tmp_path):
        p = write_toml(
            tmp_path,
            "[hardware_aliases]\nvoron = \"trident\"\n",
        )
        c = load_config(p)
        # Replaced, not merged — the author's mako/tk defaults are gone.
        assert c.hardware_aliases == {"voron": "trident"}

    def test_naming_format_override(self, tmp_path):
        p = write_toml(
            tmp_path,
            '[naming.filament]\nformat = "{brand} {material} [{hardware}]"\n',
        )
        c = load_config(p)
        assert c.naming.filament.format == "{brand} {material} [{hardware}]"
        # hardware sub-template falls back to the default
        assert c.naming.filament.hardware == "{extruder} - {hotend} - {nozzle}"
        # other categories untouched
        assert c.naming.process.format == DEFAULT_CONFIG.naming.process.format

    def test_naming_toggle_override(self, tmp_path):
        p = write_toml(tmp_path, "[naming]\npad_layer_heights = false\n")
        c = load_config(p)
        assert c.naming.pad_layer_heights is False
        assert c.naming.trim_nozzle_zeros is True


class TestValidation:
    def test_unknown_top_level_key_rejected(self, tmp_path):
        p = write_toml(tmp_path, "[thresholდs]\n")  # deliberate typo-ish section
        # A genuinely unknown top-level table:
        p = write_toml(tmp_path, "[thresholdz]\nx = 1\n")
        with pytest.raises(ConfigError, match="Unknown key"):
            load_config(p)

    def test_unknown_threshold_key_rejected(self, tmp_path):
        p = write_toml(tmp_path, "[thresholds]\nfuzz_materail = 80\n")
        with pytest.raises(ConfigError, match="Unknown key.*fuzz_materail"):
            load_config(p)

    def test_unknown_naming_category_key_rejected(self, tmp_path):
        p = write_toml(tmp_path, '[naming.filament]\nformart = "x"\n')
        with pytest.raises(ConfigError, match="Unknown key"):
            load_config(p)

    def test_non_string_alias_value_rejected(self, tmp_path):
        p = write_toml(tmp_path, "[abbreviations]\nTK = 42\n")
        with pytest.raises(ConfigError, match="string = string"):
            load_config(p)


class TestConfigChangesBehavior:
    """Prove the loaded config actually reaches the consumers, not just parses."""

    def test_custom_abbreviation_expands_in_normalization(self, tmp_path):
        from orcaslicer_cleaner.standardizer import _normalize_name

        p = write_toml(tmp_path, '[abbreviations]\nVG = "Voron Gantry"\n')
        cfg = load_config(p)
        # Default config would NOT expand "VG"; the custom one does.
        assert _normalize_name("PLA - Brand (VG - 0.4mm)") == "PLA - Brand (VG - 0.4mm)"
        assert (
            _normalize_name("PLA - Brand (VG - 0.4mm)", cfg)
            == "PLA - Brand (Voron Gantry - 0.4mm)"
        )

    def test_disabling_layer_padding_toggle(self, tmp_path):
        from orcaslicer_cleaner.standardizer import _normalize_name

        p = write_toml(tmp_path, "[naming]\npad_layer_heights = false\n")
        cfg = load_config(p)
        # Default pads to two decimals; the toggle turns that off.
        assert _normalize_name("0.2mm - Speed") == "0.20mm - Speed"
        assert _normalize_name("0.2mm - Speed", cfg) == "0.2mm - Speed"


class TestConfigViaCli:
    def test_bad_config_path_exits_2(self, tmp_path):
        from click.testing import CliRunner

        from orcaslicer_cleaner.cli import cli

        # A --profile-dir that exists (empty) so the group callback runs to config load.
        (tmp_path / "user").mkdir()
        result = CliRunner().invoke(
            cli,
            ["--profile-dir", str(tmp_path / "user"),
             "--config", str(tmp_path / "missing.toml"),
             "--system-profiles", str(tmp_path / "nope"),
             "scan"],
        )
        assert result.exit_code == 2
        assert "Config error" in result.output or "not found" in result.output

    def test_malformed_config_reports_error(self, tmp_path):
        from click.testing import CliRunner

        from orcaslicer_cleaner.cli import cli

        (tmp_path / "user").mkdir()
        bad = tmp_path / "config.toml"
        bad.write_text("[thresholds]\nfuzz_materail = 80\n", encoding="utf-8")
        result = CliRunner().invoke(
            cli,
            ["--profile-dir", str(tmp_path / "user"),
             "--config", str(bad),
             "--system-profiles", str(tmp_path / "nope"),
             "scan"],
        )
        assert result.exit_code == 2
        assert "Unknown key" in result.output
